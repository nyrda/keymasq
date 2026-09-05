"""Motion sensor calibration and reusable motion-control models."""

from dataclasses import dataclass, field

from keymasq.common.model.actions import normalize_output_id
from keymasq.common.model.analog import SAME_DEVICE_OUTPUT_ID, AnalogControlConfig
from keymasq.common.virtual_devices import virtual_gamepad_output_id

MOTION_GYRO_AXES = frozenset({"pitch", "yaw", "roll"})
MOTION_ACCELEROMETER_AXES = frozenset({"x", "y", "z"})
MOTION_AXES = MOTION_GYRO_AXES | MOTION_ACCELEROMETER_AXES
MOTION_CONTROL_MODES = frozenset(
    {"mouse", "gamepad", "tilt_mouse", "tilt_gamepad", "area_mouse", "analog"}
)
MOTION_GAMEPAD_TARGETS = frozenset({"left", "right", "analog"})
MOTION_AXIS_OUTPUTS = frozenset({"none", "horizontal", "vertical"})
MOTION_TILT_REFERENCES = frozenset({"activation", "gravity"})
MOTION_ANALOG_SOURCES = frozenset({"gyro", "tilt"})
MOTION_ANALOG_AXES = frozenset({"none", "yaw", "pitch", "roll"})
MOTION_NORMALIZATION_VERSION = 3

_DRIVER_FAMILIES = {
    "hid-nintendo": "nintendo",
    "nintendo": "nintendo",
    "hid-playstation": "playstation",
    "playstation": "playstation",
    "hid-steam": "steam",
    "steam": "steam",
}

_MOTION_AXIS_LAYOUTS: dict[str, dict[str, dict[str, tuple[str, bool]]]] = {
    "playstation": {
        "gyro": {
            "abs_rx": ("pitch", False),
            "abs_ry": ("yaw", False),
            "abs_rz": ("roll", False),
        },
        "accelerometer": {
            "abs_x": ("x", False),
            "abs_y": ("y", False),
            "abs_z": ("z", False),
        },
    },
    "nintendo": {
        "gyro": {
            "abs_rx": ("roll", True),
            "abs_ry": ("pitch", True),
            "abs_rz": ("yaw", False),
        },
        "accelerometer": {
            "abs_x": ("y", False),
            "abs_y": ("x", True),
            "abs_z": ("z", False),
        },
    },
    "steam": {
        "gyro": {
            "abs_rx": ("pitch", False),
            "abs_ry": ("yaw", False),
            "abs_rz": ("roll", False),
        },
        "accelerometer": {
            "abs_x": ("x", False),
            "abs_y": ("y", False),
            "abs_z": ("z", False),
        },
    },
}

_LEGACY_NINTENDO_ACCELEROMETER_LAYOUT_V2 = {
    "abs_x": ("z", True),
    "abs_y": ("x", True),
    "abs_z": ("y", False),
}


@dataclass
class MotionAxisDefinition:
    """Convert one raw evdev axis into a canonical physical axis."""

    role: str
    evdev: str
    evdev_code: int | None = None
    offset: float = 0.0
    scale: float = 1.0
    invert: bool = False
    noise: float = 0.0

    def __post_init__(self) -> None:
        self.role = str(self.role or "").strip().lower()
        if self.role not in MOTION_AXES:
            raise ValueError("motion axis role must be pitch, yaw, roll, x, y, or z")
        self.evdev = str(self.evdev or "").strip().lower()
        if not self.evdev:
            raise ValueError("motion axis evdev name is required")
        self.offset = float(self.offset)
        self.scale = float(self.scale)
        if self.scale <= 0.0:
            raise ValueError("motion axis scale must be positive")
        self.noise = max(0.0, float(self.noise))


@dataclass
class MotionSensorDefinition:
    """Hardware-owned description and calibration of one motion sensor."""

    id: str
    label: str
    source: str | None = None
    driver: str | None = None
    gyro_axes: list[MotionAxisDefinition] = field(default_factory=list)
    accelerometer_axes: list[MotionAxisDefinition] = field(default_factory=list)
    calibration_version: int = MOTION_NORMALIZATION_VERSION
    calibrated_at: str | None = None
    calibration_samples: int = 0

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        if not self.id:
            raise ValueError("motion sensor id is required")
        self.label = str(self.label or self.id).strip() or self.id
        self.source = str(self.source).strip() if self.source else None
        self.driver = str(self.driver).strip() if self.driver else None
        self.calibration_version = max(1, int(self.calibration_version))
        self.calibration_samples = max(0, int(self.calibration_samples))


@dataclass
class MotionAxisRoutingConfig:
    """Assign each canonical gyro axis to a two-dimensional output channel."""

    yaw: str = "horizontal"
    pitch: str = "vertical"
    roll: str = "horizontal"

    def __post_init__(self) -> None:
        self.yaw = _motion_axis_output(self.yaw, "horizontal")
        self.pitch = _motion_axis_output(self.pitch, "vertical")
        self.roll = _motion_axis_output(self.roll, "horizontal")


@dataclass
class MotionMouseConfig:
    sensitivity_x: float = 8.0
    sensitivity_y: float = 8.0
    deadzone_dps: float = 0.5
    smoothing: float = 0.15
    response_curve: float = 1.0
    invert_x: bool = False
    invert_y: bool = False

    def __post_init__(self) -> None:
        self.sensitivity_x = max(0.0, float(self.sensitivity_x))
        self.sensitivity_y = max(0.0, float(self.sensitivity_y))
        self.deadzone_dps = max(0.0, float(self.deadzone_dps))
        self.smoothing = max(0.0, min(0.99, float(self.smoothing)))
        self.response_curve = max(0.1, min(4.0, float(self.response_curve)))


@dataclass
class MotionGamepadConfig:
    output_id: str | None = SAME_DEVICE_OUTPUT_ID
    target: str = "right"
    target_analog_id: str | None = None
    max_rate_dps: float = 90.0
    deadzone_dps: float = 0.0
    minimum_output: float = 0.25
    smoothing: float = 0.15
    response_curve: float = 1.0
    invert_x: bool = False
    invert_y: bool = False

    def __post_init__(self) -> None:
        self.output_id = normalize_output_id(self.output_id) or virtual_gamepad_output_id(1)
        self.target = str(self.target or "right").strip().lower()
        if self.target not in MOTION_GAMEPAD_TARGETS:
            self.target = "right"
        self.target_analog_id = normalize_output_id(self.target_analog_id)
        if self.target != "analog":
            self.target_analog_id = None
        self.max_rate_dps = max(1.0, float(self.max_rate_dps))
        self.minimum_output = max(0.0, min(1.0, float(self.minimum_output)))
        self.deadzone_dps = max(0.0, float(self.deadzone_dps))
        self.smoothing = max(0.0, min(0.99, float(self.smoothing)))
        self.response_curve = max(0.1, min(4.0, float(self.response_curve)))


@dataclass
class MotionTiltConfig:
    """Profile-owned tuning for accelerometer-derived controller tilt."""

    reference: str = "activation"
    pitch: str = "vertical"
    roll: str = "horizontal"
    deadzone_deg: float = 2.0
    full_scale_deg: float = 30.0
    smoothing: float = 0.8
    response_curve: float = 1.0
    invert_x: bool = False
    invert_y: bool = False
    speed_x: float = 900.0
    speed_y: float = 900.0
    area_radius_x: float = 400.0
    area_radius_y: float = 400.0
    drag_center: bool = True

    def __post_init__(self) -> None:
        self.reference = str(self.reference or "activation").strip().lower()
        if self.reference not in MOTION_TILT_REFERENCES:
            self.reference = "activation"
        self.pitch = _motion_axis_output(self.pitch, "vertical")
        self.roll = _motion_axis_output(self.roll, "horizontal")
        self.deadzone_deg = max(0.0, min(89.0, float(self.deadzone_deg)))
        self.full_scale_deg = max(
            self.deadzone_deg + 0.1,
            min(90.0, float(self.full_scale_deg)),
        )
        self.smoothing = max(0.0, min(0.99, float(self.smoothing)))
        self.response_curve = max(0.1, min(4.0, float(self.response_curve)))
        self.speed_x = max(0.0, float(self.speed_x))
        self.speed_y = max(0.0, float(self.speed_y))
        self.area_radius_x = max(0.0, float(self.area_radius_x))
        self.area_radius_y = max(0.0, float(self.area_radius_y))


@dataclass
class MotionAnalogConfig:
    """Turn one or two normalized motion signals into an Analog Control input."""

    analog_control_name: str | None = None
    analog_control_config: AnalogControlConfig | None = None
    source: str = "tilt"
    x_axis: str = "roll"
    y_axis: str = "pitch"
    reference: str = "activation"
    full_scale_dps: float = 360.0
    full_scale_deg: float = 30.0
    smoothing: float = 0.15
    invert_x: bool = False
    invert_y: bool = False

    def __post_init__(self) -> None:
        self.analog_control_name = (
            str(self.analog_control_name).strip() if self.analog_control_name else None
        ) or None
        self.source = str(self.source or "tilt").strip().lower()
        if self.source not in MOTION_ANALOG_SOURCES:
            self.source = "tilt"
        self.x_axis = _motion_analog_axis(self.x_axis, "roll")
        self.y_axis = _motion_analog_axis(self.y_axis, "pitch")
        if self.source == "tilt":
            if self.x_axis == "yaw":
                self.x_axis = "roll"
            if self.y_axis == "yaw":
                self.y_axis = "pitch"
        self.reference = str(self.reference or "activation").strip().lower()
        if self.reference not in MOTION_TILT_REFERENCES:
            self.reference = "activation"
        self.full_scale_dps = max(1.0, float(self.full_scale_dps))
        self.full_scale_deg = max(0.1, min(90.0, float(self.full_scale_deg)))
        self.smoothing = max(0.0, min(0.99, float(self.smoothing)))


@dataclass
class MotionControlConfig:
    name: str
    description: str | None = None
    mode: str = "mouse"
    axis_routing: MotionAxisRoutingConfig = field(default_factory=MotionAxisRoutingConfig)
    mouse: MotionMouseConfig = field(default_factory=MotionMouseConfig)
    gamepad: MotionGamepadConfig = field(default_factory=MotionGamepadConfig)
    tilt: MotionTiltConfig = field(default_factory=MotionTiltConfig)
    analog: MotionAnalogConfig = field(default_factory=MotionAnalogConfig)

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        if not self.name:
            raise ValueError("motion control name is required")
        self.mode = str(self.mode or "mouse").strip().lower()
        if self.mode not in MOTION_CONTROL_MODES:
            raise ValueError(
                "motion control mode must be mouse, gamepad, tilt_mouse, "
                "tilt_gamepad, area_mouse, or analog"
            )


def _motion_axis_output(value: object, default: str) -> str:
    output = str(value or default).strip().lower()
    return output if output in MOTION_AXIS_OUTPUTS else default


def _motion_analog_axis(value: object, default: str) -> str:
    axis = str(value or default).strip().lower()
    return axis if axis in MOTION_ANALOG_AXES else default


def canonical_motion_axis(
    driver: str | None,
    kind: str,
    evdev_name: str,
    *,
    normalization_version: int | None = None,
) -> tuple[str, bool] | None:
    """Return the canonical axis role and sign correction for a kernel driver."""
    family = _DRIVER_FAMILIES.get(str(driver or "").strip().lower(), "playstation")
    normalized_evdev = evdev_name.strip().lower()
    if (
        family == "nintendo"
        and kind == "accelerometer"
        and normalization_version is not None
        and normalization_version <= 2
    ):
        return _LEGACY_NINTENDO_ACCELEROMETER_LAYOUT_V2.get(normalized_evdev)
    return _MOTION_AXIS_LAYOUTS[family].get(kind, {}).get(normalized_evdev)
