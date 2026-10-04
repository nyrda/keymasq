"""Per-profile pointer movement factors and controller-axis output."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from keymasq.common.coercion import coerce_bool, coerce_float, coerce_int
from keymasq.common.gamepad_axes import normalize_gamepad_axis_target
from keymasq.common.model.core import DeviceType

if TYPE_CHECKING:
    from keymasq.common.model.hardware import EvdevDevice, HardwareConfig

POINTER_SOURCE_ID = "pointer"
POINTER_MODES = ("mouse", "axes")
POINTER_BEHAVIORS = ("velocity", "position")
POINTER_OVERSHOOT_MODES = ("drag", "keep")
POINTER_AXIS_DIRECTIONS = ("both", "max", "min")
POINTER_AXIS_NONE = "none"

MAX_POINTER_FACTOR = 50.0
MIN_POINTER_WINDOW_MS = 4
MAX_POINTER_WINDOW_MS = 250
MAX_POINTER_RECENTER_MS = 60_000
MAX_POINTER_COUNTS = 1_000_000.0

_POINTER_CHOICES: dict[str, tuple[str, ...]] = {
    "mode": POINTER_MODES,
    "behavior": POINTER_BEHAVIORS,
    "overshoot": POINTER_OVERSHOOT_MODES,
    "x_direction": POINTER_AXIS_DIRECTIONS,
    "y_direction": POINTER_AXIS_DIRECTIONS,
}
_POINTER_RANGES: dict[str, tuple[float, float]] = {
    "factor_x": (0.0, MAX_POINTER_FACTOR),
    "factor_y": (0.0, MAX_POINTER_FACTOR),
    "full_speed": (1.0, MAX_POINTER_COUNTS),
    "window_ms": (MIN_POINTER_WINDOW_MS, MAX_POINTER_WINDOW_MS),
    "radius": (1.0, MAX_POINTER_COUNTS),
    "recenter_ms": (0, MAX_POINTER_RECENTER_MS),
    "deadzone": (0.0, 0.95),
    "minimum_output": (0.0, 0.95),
    "response_curve": (0.25, 4.0),
}
_POINTER_INT_FIELDS = frozenset({"window_ms", "recenter_ms"})


@dataclass
class PointerMovementConfig:
    """How one mouse's relative movement is scaled or turned into controller axes."""

    mode: str = "mouse"
    factor_x: float = 1.0
    factor_y: float = 1.0
    invert_x: bool = False
    invert_y: bool = False
    swap_axes: bool = False
    output_id: str | None = None
    behavior: str = "velocity"
    full_speed: float = 4000.0
    window_ms: int = 20
    radius: float = 1500.0
    overshoot: str = "drag"
    recenter_ms: int = 0
    x_axis: str | None = "abs_rx"
    y_axis: str | None = "abs_ry"
    x_direction: str = "both"
    y_direction: str = "both"
    deadzone: float = 0.0
    minimum_output: float = 0.0
    response_curve: float = 1.0

    def __post_init__(self) -> None:
        self.mode = _choice(self.mode, POINTER_MODES)
        self.factor_x = _ranged(self.factor_x, 1.0, "factor_x")
        self.factor_y = _ranged(self.factor_y, 1.0, "factor_y")
        self.invert_x = coerce_bool(self.invert_x)
        self.invert_y = coerce_bool(self.invert_y)
        self.swap_axes = coerce_bool(self.swap_axes)
        self.output_id = str(self.output_id or "").strip() or None
        self.behavior = _choice(self.behavior, POINTER_BEHAVIORS)
        self.full_speed = _ranged(self.full_speed, 4000.0, "full_speed")
        self.window_ms = int(_ranged(coerce_int(self.window_ms, 20), 20, "window_ms"))
        self.radius = _ranged(self.radius, 1500.0, "radius")
        self.overshoot = _choice(self.overshoot, POINTER_OVERSHOOT_MODES)
        self.recenter_ms = int(_ranged(coerce_int(self.recenter_ms, 0), 0, "recenter_ms"))
        self.x_axis = pointer_output_axis(self.x_axis)
        self.y_axis = pointer_output_axis(self.y_axis)
        self.x_direction = _choice(self.x_direction, POINTER_AXIS_DIRECTIONS)
        self.y_direction = _choice(self.y_direction, POINTER_AXIS_DIRECTIONS)
        self.deadzone = _ranged(self.deadzone, 0.0, "deadzone")
        self.minimum_output = _ranged(self.minimum_output, 0.0, "minimum_output")
        self.response_curve = _ranged(self.response_curve, 1.0, "response_curve")


def is_pointer_interface(device: "EvdevDevice") -> bool:
    return device.device_type == DeviceType.MOUSE or {"rel_x", "rel_y"} <= {
        str(name).lower() for name in device.capabilities
    }


def pointer_id_claimed(hardware: "HardwareConfig") -> bool:
    """Whether a configured control, possibly older than the pointer source, owns its ID."""
    controls = (*hardware.buttons, *hardware.analog_inputs, *hardware.motion_sensors)
    return any(control.id == POINTER_SOURCE_ID for control in controls)


def pointer_interface_ids(hardware: "HardwareConfig") -> list[str]:
    """Return configured interfaces that report relative pointer movement."""
    if pointer_id_claimed(hardware):
        return []
    return [
        device.id for device in hardware.evdev_devices if device.id and is_pointer_interface(device)
    ]


def pointer_output_axis(value: object) -> str | None:
    if str(value or "").strip().lower() in {"", POINTER_AXIS_NONE}:
        return None
    return normalize_gamepad_axis_target(value)


def pointer_movement_to_dict(config: PointerMovementConfig) -> dict[str, object]:
    data: dict[str, object] = {
        "mode": config.mode,
        "factor_x": float(config.factor_x),
        "factor_y": float(config.factor_y),
        "invert_x": bool(config.invert_x),
        "invert_y": bool(config.invert_y),
        "swap_axes": bool(config.swap_axes),
        "behavior": config.behavior,
        "full_speed": float(config.full_speed),
        "window_ms": int(config.window_ms),
        "radius": float(config.radius),
        "overshoot": config.overshoot,
        "recenter_ms": int(config.recenter_ms),
        "x_axis": config.x_axis or POINTER_AXIS_NONE,
        "y_axis": config.y_axis or POINTER_AXIS_NONE,
        "x_direction": config.x_direction,
        "y_direction": config.y_direction,
        "deadzone": float(config.deadzone),
        "minimum_output": float(config.minimum_output),
        "response_curve": float(config.response_curve),
    }
    if config.output_id:
        data["output_id"] = config.output_id
    return data


def pointer_movement_from_dict(data: dict[str, object]) -> PointerMovementConfig:
    defaults = PointerMovementConfig()
    return PointerMovementConfig(
        mode=str(data.get("mode", defaults.mode)),
        factor_x=coerce_float(data.get("factor_x"), defaults.factor_x),
        factor_y=coerce_float(data.get("factor_y"), defaults.factor_y),
        invert_x=coerce_bool(data.get("invert_x")),
        invert_y=coerce_bool(data.get("invert_y")),
        swap_axes=coerce_bool(data.get("swap_axes")),
        output_id=str(data.get("output_id") or "") or None,
        behavior=str(data.get("behavior", defaults.behavior)),
        full_speed=coerce_float(data.get("full_speed"), defaults.full_speed),
        window_ms=coerce_int(data.get("window_ms"), defaults.window_ms),
        radius=coerce_float(data.get("radius"), defaults.radius),
        overshoot=str(data.get("overshoot", defaults.overshoot)),
        recenter_ms=coerce_int(data.get("recenter_ms"), defaults.recenter_ms),
        x_axis=str(data.get("x_axis", defaults.x_axis)),
        y_axis=str(data.get("y_axis", defaults.y_axis)),
        x_direction=str(data.get("x_direction", defaults.x_direction)),
        y_direction=str(data.get("y_direction", defaults.y_direction)),
        deadzone=coerce_float(data.get("deadzone"), defaults.deadzone),
        minimum_output=coerce_float(data.get("minimum_output"), defaults.minimum_output),
        response_curve=coerce_float(data.get("response_curve"), defaults.response_curve),
    )


def pointer_movement_from_toml(data: Mapping[str, object]) -> PointerMovementConfig:
    """Parse a profile file mapping, rejecting values that would otherwise be replaced."""
    for key, choices in _POINTER_CHOICES.items():
        value = data.get(key)
        if value is not None and (
            not isinstance(value, str) or value.strip().lower() not in choices
        ):
            allowed = ", ".join(map(repr, choices))
            raise ValueError(f"pointer movement {key} must be one of {allowed}, got {value!r}")
    for key, (minimum, maximum) in _POINTER_RANGES.items():
        value = data.get(key)
        if value is None:
            continue
        integer = key in _POINTER_INT_FIELDS
        if (
            isinstance(value, bool)
            or not isinstance(value, int if integer else int | float)
            or not minimum <= value <= maximum
        ):
            kind = "an integer" if integer else "a number"
            raise ValueError(
                f"pointer movement {key} must be {kind} from {minimum:g} to {maximum:g}, "
                f"got {value!r}"
            )
    for key in ("x_axis", "y_axis"):
        value = data.get(key)
        if value is not None and (
            not isinstance(value, str)
            or (
                value.strip().lower() not in {"", POINTER_AXIS_NONE}
                and normalize_gamepad_axis_target(value) is None
            )
        ):
            raise ValueError(
                f"pointer movement {key} must be an ABS axis name or 'none', got {value!r}"
            )
    for key in ("invert_x", "invert_y", "swap_axes"):
        value = data.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"pointer movement {key} must be true or false, got {value!r}")
    output_id = data.get("output_id")
    if output_id is not None and not isinstance(output_id, str):
        raise ValueError(f"pointer movement output_id must be a string, got {output_id!r}")
    return pointer_movement_from_dict(dict(data))


def _choice(value: object, choices: tuple[str, ...]) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in choices else choices[0]


def _ranged(value: object, default: float, key: str) -> float:
    minimum, maximum = _POINTER_RANGES[key]
    return max(minimum, min(maximum, coerce_float(value, default)))
