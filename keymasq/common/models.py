import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast, overload

from keymasq.common.coercion import coerce_int
from keymasq.common.gamepad_axes import clamp_gamepad_axis_value, normalize_gamepad_axis_target

if TYPE_CHECKING:
    from keymasq.keymasqd.superkey_state import SuperkeyConfig as RuntimeSuperkeyConfig

log = logging.getLogger("keymasq.common.models")
PROTECTED_BUTTONS = frozenset({"btn_left", "btn_right"})


class ActionType(Enum):
    PASSTHROUGH = "passthrough"
    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    GAMEPAD = "gamepad"
    GAMEPAD_AXIS = "gamepad_axis"
    ANALOG_CONTROL = "analog_control"
    EXEC = "exec"
    COMPOSITOR_DISPATCH = "compositor_dispatch"
    SUPPRESS = "suppress"
    SUPERKEY = "superkey"
    START_MACRO_RECORDING = "start_macro_recording"
    STOP_MACRO_RECORDING = "stop_macro_recording"
    PLAY_MACRO_SLOT = "play_macro_slot"
    CANCEL_MACRO_PLAYBACK = "cancel_macro_playback"
    EMERGENCY_RESET = "emergency_reset"
    MACRO = "macro"
    MOUSE_MOVE_REL = "mouse_move_rel"
    MOUSE_MOVE_ABS = "mouse_move_abs"
    PROFILE_ENABLE = "profile_enable"
    PROFILE_DISABLE = "profile_disable"
    PROFILE_TOGGLE = "profile_toggle"
    MPRIS = "mpris"
    REPEAT = "repeat"


MPRIS_COMMAND_PLAY_PAUSE = "play_pause"
MPRIS_COMMAND_PAUSE = "pause"
MPRIS_COMMAND_PLAY = "play"
MPRIS_COMMAND_NEXT = "next"
MPRIS_COMMAND_PREVIOUS = "previous"
MPRIS_COMMAND_STOP = "stop"
MPRIS_COMMANDS = frozenset(
    {
        MPRIS_COMMAND_PLAY_PAUSE,
        MPRIS_COMMAND_PAUSE,
        MPRIS_COMMAND_PLAY,
        MPRIS_COMMAND_NEXT,
        MPRIS_COMMAND_PREVIOUS,
        MPRIS_COMMAND_STOP,
    }
)
DEFAULT_MPRIS_COMMAND = MPRIS_COMMAND_PLAY_PAUSE
_MPRIS_COMMAND_ALIASES = {
    "playpause": MPRIS_COMMAND_PLAY_PAUSE,
    "play-pause": MPRIS_COMMAND_PLAY_PAUSE,
    "play/pause": MPRIS_COMMAND_PLAY_PAUSE,
    "toggle": MPRIS_COMMAND_PLAY_PAUSE,
    "prev": MPRIS_COMMAND_PREVIOUS,
}


def parse_mpris_command(value: object) -> str | None:
    command = str(value or "").strip().lower().replace("-", "_")
    command = _MPRIS_COMMAND_ALIASES.get(command, command)
    if command in MPRIS_COMMANDS:
        return command
    return None


def normalize_mpris_command(value: object) -> str:
    command = parse_mpris_command(value)
    if command is not None:
        return command
    return DEFAULT_MPRIS_COMMAND


REPEAT_CATEGORY_KEYBOARD = "keyboard"
REPEAT_CATEGORY_MOUSE = "mouse"
REPEAT_CATEGORY_GAMEPAD = "gamepad"
REPEAT_CATEGORY_MACRO = "macro"
REPEAT_CATEGORY_SPECIAL = "special"

REPEAT_CATEGORIES = frozenset(
    {
        REPEAT_CATEGORY_KEYBOARD,
        REPEAT_CATEGORY_MOUSE,
        REPEAT_CATEGORY_GAMEPAD,
        REPEAT_CATEGORY_MACRO,
        REPEAT_CATEGORY_SPECIAL,
    }
)
DEFAULT_REPEAT_CATEGORIES = (
    REPEAT_CATEGORY_KEYBOARD,
    REPEAT_CATEGORY_MOUSE,
    REPEAT_CATEGORY_GAMEPAD,
    REPEAT_CATEGORY_MACRO,
    REPEAT_CATEGORY_SPECIAL,
)
_LEGACY_REPEAT_CATEGORY_ALIASES = {
    "mouse_button": REPEAT_CATEGORY_MOUSE,
    "mouse_wheel": REPEAT_CATEGORY_MOUSE,
}


class SuperkeyMode(Enum):
    PATTERN = "pattern"
    OVERLOAD = "overload"


SUPERKEY_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
        ActionType.EXEC,
        ActionType.COMPOSITOR_DISPATCH,
        ActionType.START_MACRO_RECORDING,
        ActionType.STOP_MACRO_RECORDING,
        ActionType.PLAY_MACRO_SLOT,
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.EMERGENCY_RESET,
        ActionType.MACRO,
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
        ActionType.MPRIS,
    }
)

RAPIDFIRE_ACTION_TYPES = frozenset(
    {
        ActionType.KEYBOARD,
        ActionType.MOUSE,
        ActionType.GAMEPAD,
        ActionType.GAMEPAD_AXIS,
        ActionType.MOUSE_MOVE_REL,
        ActionType.MOUSE_MOVE_ABS,
        ActionType.REPEAT,
    }
)

DEFAULT_RAPIDFIRE_HOLD_MS = 20
DEFAULT_RAPIDFIRE_WAIT_MS = 20
MIN_RAPIDFIRE_HOLD_MS = 0
MIN_RAPIDFIRE_WAIT_MS = 1

MACRO_LOOP_STOP_BEHAVIORS = frozenset({"finish_run", "cancel_run"})
DEFAULT_MACRO_LOOP_STOP_BEHAVIOR = "finish_run"
MAX_MACRO_RECORDING_SLOTS = 4


def normalize_macro_recording_slot(value: object) -> int:
    try:
        slot = int(cast(int | float | str, value))
    except (TypeError, ValueError):
        return 0
    if 1 <= slot <= MAX_MACRO_RECORDING_SLOTS:
        return slot
    return 0


class DeviceType(Enum):
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    GAMEPAD = "gamepad"
    OTHER = "other"


class ProfileState(Enum):
    INACTIVE = "inactive"
    WAITING = "waiting"
    ACTIVE = "active"
    STANDBY = "standby"


class WindowFieldType(Enum):
    CLASS = "class"
    TITLE = "title"
    INITIAL_CLASS = "initial_class"
    INITIAL_TITLE = "initial_title"
    TAG = "tag"


def is_protected_button(button_id: str) -> bool:
    return button_id.lower() in PROTECTED_BUTTONS


def action_type_supports_rapidfire(action_type: ActionType) -> bool:
    return action_type in RAPIDFIRE_ACTION_TYPES


def clamp_rapidfire_hold_ms(rapidfire_hold_ms: int) -> int:
    return max(MIN_RAPIDFIRE_HOLD_MS, int(rapidfire_hold_ms))


def clamp_rapidfire_wait_ms(rapidfire_wait_ms: int) -> int:
    return max(MIN_RAPIDFIRE_WAIT_MS, int(rapidfire_wait_ms))


def normalize_rapidfire_fields(
    action_type: ActionType,
    *,
    rapidfire_enabled: bool,
    rapidfire_hold_ms: int,
    rapidfire_wait_ms: int,
) -> tuple[bool, int, int]:
    if not action_type_supports_rapidfire(action_type):
        return False, DEFAULT_RAPIDFIRE_HOLD_MS, DEFAULT_RAPIDFIRE_WAIT_MS
    return (
        rapidfire_enabled,
        clamp_rapidfire_hold_ms(rapidfire_hold_ms),
        clamp_rapidfire_wait_ms(rapidfire_wait_ms),
    )


def resolve_rapidfire_fields(
    action_type: ActionType,
    *,
    rapidfire_enabled: bool,
    rapidfire_hold_ms: int,
    rapidfire_wait_ms: int,
) -> tuple[bool, int, int, bool]:
    unsupported_requested = rapidfire_enabled and not action_type_supports_rapidfire(action_type)
    normalized_enabled, normalized_hold_ms, normalized_wait_ms = normalize_rapidfire_fields(
        action_type,
        rapidfire_enabled=rapidfire_enabled,
        rapidfire_hold_ms=rapidfire_hold_ms,
        rapidfire_wait_ms=rapidfire_wait_ms,
    )
    return (
        normalized_enabled,
        normalized_hold_ms,
        normalized_wait_ms,
        unsupported_requested,
    )


def parse_rapidfire_fields(
    action_type: ActionType,
    *,
    rapidfire_enabled: object,
    rapidfire_hold_ms: object,
    rapidfire_wait_ms: object,
) -> tuple[bool, int, int, bool]:
    parsed_hold_ms = coerce_int(rapidfire_hold_ms, DEFAULT_RAPIDFIRE_HOLD_MS)
    parsed_wait_ms = coerce_int(rapidfire_wait_ms, DEFAULT_RAPIDFIRE_WAIT_MS)
    return resolve_rapidfire_fields(
        action_type,
        rapidfire_enabled=bool(rapidfire_enabled),
        rapidfire_hold_ms=parsed_hold_ms,
        rapidfire_wait_ms=parsed_wait_ms,
    )


def normalize_macro_loop_stop_behavior(value: object) -> str:
    behavior = str(value or DEFAULT_MACRO_LOOP_STOP_BEHAVIOR).lower()
    if behavior in MACRO_LOOP_STOP_BEHAVIORS:
        return behavior
    return DEFAULT_MACRO_LOOP_STOP_BEHAVIOR


def normalize_gamepad_output_id(action_type: ActionType, output_id: object) -> str | None:
    if action_type not in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS):
        return None
    return normalize_output_id(output_id)


def normalize_output_id(output_id: object) -> str | None:
    if output_id is None:
        return None
    normalized = str(output_id).strip()
    return normalized or None


def normalize_repeat_categories(categories: object) -> list[str]:
    if categories is None:
        return list(DEFAULT_REPEAT_CATEGORIES)
    if isinstance(categories, str):
        raw_values: list[object] = [categories]
    elif isinstance(categories, (list, tuple, set, frozenset)):
        raw_values = list(categories)
    else:
        log.warning(
            "Invalid repeat_categories %r; using default repeat categories",
            categories,
        )
        return list(DEFAULT_REPEAT_CATEGORIES)
    if not raw_values:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        category = str(value or "").strip().lower()
        category = _LEGACY_REPEAT_CATEGORY_ALIASES.get(category, category)
        if category not in REPEAT_CATEGORIES or category in seen:
            continue
        normalized.append(category)
        seen.add(category)
    if not normalized:
        log.warning(
            "Invalid repeat_categories %r; using default repeat categories",
            categories,
        )
        return list(DEFAULT_REPEAT_CATEGORIES)
    return normalized


@dataclass
class ProfileDeactivationPolicy:
    on_trigger_end: bool = False
    after_actions: int | None = None
    timeout_ms: int | None = None

    @property
    def has_condition(self) -> bool:
        return bool(
            self.on_trigger_end
            or (self.after_actions is not None and self.after_actions > 0)
            or (self.timeout_ms is not None and self.timeout_ms > 0)
        )


def _positive_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(cast(int | float | str | bytes, value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_profile_deactivation_policy(
    action_type: ActionType,
    policy: ProfileDeactivationPolicy | None,
) -> ProfileDeactivationPolicy | None:
    if action_type not in (ActionType.PROFILE_ENABLE, ActionType.PROFILE_TOGGLE):
        return None
    if policy is None:
        return None

    normalized = ProfileDeactivationPolicy(
        on_trigger_end=bool(policy.on_trigger_end),
        after_actions=_positive_int_or_none(policy.after_actions),
        timeout_ms=_positive_int_or_none(policy.timeout_ms),
    )
    return normalized if normalized.has_condition else None


def parse_profile_deactivation_policy(data: object) -> ProfileDeactivationPolicy | None:
    if not isinstance(data, dict):
        return None
    payload = cast(dict[str, object], data)
    policy = ProfileDeactivationPolicy(
        on_trigger_end=bool(payload.get("on_trigger_end", False)),
        after_actions=_positive_int_or_none(payload.get("after_actions")),
        timeout_ms=_positive_int_or_none(payload.get("timeout_ms")),
    )
    return policy if policy.has_condition else None


def profile_deactivation_policy_to_dict(
    policy: ProfileDeactivationPolicy | None,
) -> dict[str, object] | None:
    if policy is None:
        return None
    normalized = ProfileDeactivationPolicy(
        on_trigger_end=bool(policy.on_trigger_end),
        after_actions=_positive_int_or_none(policy.after_actions),
        timeout_ms=_positive_int_or_none(policy.timeout_ms),
    )
    if not normalized.has_condition:
        return None
    data: dict[str, object] = {}
    if normalized.on_trigger_end:
        data["on_trigger_end"] = True
    if normalized.after_actions is not None:
        data["after_actions"] = int(normalized.after_actions)
    if normalized.timeout_ms is not None:
        data["timeout_ms"] = int(normalized.timeout_ms)
    return data


@dataclass
class EvdevDevice:
    path: str
    device_type: DeviceType
    id: str | None = None
    phys: str | None = None
    capabilities: list[str] = field(default_factory=list)


@dataclass
class ButtonDefinition:
    id: str
    label: str
    evdev: str
    evdev_code: int | None = None
    evdev_value: int | None = None
    source: str | None = None
    zone: str | None = None
    row: int | None = None
    col: int | None = None
    type: str | None = None


@dataclass
class AnalogAxisDefinition:
    role: str
    evdev: str
    evdev_code: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    center: int | None = None
    rest: int | None = None
    invert: bool = False


@dataclass
class AnalogInputDefinition:
    id: str
    label: str
    type: str
    source: str | None = None
    axes: list[AnalogAxisDefinition] = field(default_factory=list)


@dataclass
class HardwareConfig:
    vendor_id: str
    product_id: str
    name: str
    evdev_devices: list[EvdevDevice]
    buttons: list[ButtonDefinition]
    analog_inputs: list[AnalogInputDefinition] = field(default_factory=list)
    image: str | None = None
    id: str | None = None

    @property
    def hardware_id(self) -> str:
        return self.id or f"{self.vendor_id}:{self.product_id}"

    @property
    def model_id(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"


@dataclass
class MappingAction:
    action_type: ActionType
    target: str | None = None
    output_id: str | None = None
    keys: list[str] | None = None
    cmd: str | None = None
    exec_ref: int | None = None
    superkey_name: str | None = None
    superkey_config: "SuperkeyConfig | None" = None
    analog_control_name: str | None = None
    analog_control_names: list[str] = field(default_factory=list)
    analog_control_config: "AnalogControlConfig | None" = None
    analog_control_configs: list["AnalogControlConfig"] = field(default_factory=list)
    macro_name: str | None = None
    macro_events: list[dict[str, object]] | None = None
    macro_replay_mouse_movement: bool = True
    macro_replay_mouse_clicks: bool = True
    macro_speed: float = 1.0
    macro_loop_mode: str = "none"
    macro_loop_count: int = 1
    macro_loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
    macro_move_to_start: bool = False
    macro_start_x: int = 0
    macro_start_y: int = 0
    macro_block_mouse_movement: bool = False
    macro_recording_slot: int = 0
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
    mpris_command: str | None = None
    move_x: int = 0
    move_y: int = 0
    axis_value: int = 0
    move_speed: float = 1.0
    move_jitter: float = 0.3

    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = DEFAULT_RAPIDFIRE_HOLD_MS
    rapidfire_wait_ms: int = DEFAULT_RAPIDFIRE_WAIT_MS

    tap_enabled: bool = False
    tap_hold_ms: int = 10
    profile_deactivation: ProfileDeactivationPolicy | None = None
    source_profile_name: str | None = None
    repeat_categories: list[str] | None = None

    def __post_init__(self) -> None:
        self.source_profile_name = (
            str(self.source_profile_name).strip() if self.source_profile_name else None
        ) or None
        self.output_id = normalize_gamepad_output_id(self.action_type, self.output_id)
        if self.analog_control_name and not self.analog_control_names:
            self.analog_control_names = [self.analog_control_name]
        else:
            self.analog_control_names = [
                str(name).strip() for name in self.analog_control_names if str(name).strip()
            ]
            if self.analog_control_names:
                self.analog_control_name = self.analog_control_names[0]
        if self.analog_control_config and not self.analog_control_configs:
            self.analog_control_configs = [self.analog_control_config]
        elif self.analog_control_configs:
            self.analog_control_config = self.analog_control_configs[0]
        if self.action_type == ActionType.GAMEPAD_AXIS:
            self.target = normalize_gamepad_axis_target(self.target)
            self.axis_value = clamp_gamepad_axis_value(self.target, self.axis_value)
        rapidfire_enabled, rapidfire_hold_ms, rapidfire_wait_ms = normalize_rapidfire_fields(
            self.action_type,
            rapidfire_enabled=bool(self.rapidfire_enabled),
            rapidfire_hold_ms=int(self.rapidfire_hold_ms),
            rapidfire_wait_ms=int(self.rapidfire_wait_ms),
        )
        self.rapidfire_enabled = rapidfire_enabled
        self.rapidfire_hold_ms = rapidfire_hold_ms
        self.rapidfire_wait_ms = rapidfire_wait_ms
        self.profile_deactivation = normalize_profile_deactivation_policy(
            self.action_type,
            self.profile_deactivation,
        )
        if self.action_type == ActionType.MPRIS:
            self.mpris_command = normalize_mpris_command(self.mpris_command)
        else:
            self.mpris_command = None
        if self.action_type in {
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.PLAY_MACRO_SLOT,
        }:
            self.macro_recording_slot = normalize_macro_recording_slot(self.macro_recording_slot)
        else:
            self.macro_recording_slot = 0
        if self.action_type == ActionType.REPEAT:
            self.repeat_categories = normalize_repeat_categories(self.repeat_categories)
        else:
            self.repeat_categories = None


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
SAME_DEVICE_OUTPUT_ID = "same-device"
ANALOG_GAMEPAD_OUTPUT_TARGETS = frozenset({"same", "left", "right", "analog"})
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
    output_rest: int | None = None
    output_direction: str = ""
    output_invert: bool = False
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
        if self.output_rest is not None:
            self.output_rest = int(self.output_rest)
        self.output_invert = bool(self.output_invert)
        self.output_direction = str(self.output_direction or "").lower()
        if self.output_direction not in ANALOG_GAMEPAD_OUTPUT_DIRECTIONS:
            self.output_direction = "min" if self.output_invert else "max"
        self.output_invert = self.output_direction == "min"
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


@dataclass
class SuperkeyAction:
    action_type: ActionType
    target: str | None = None
    output_id: str | None = None
    cmd: str | None = None
    exec_ref: int | None = None
    macro_name: str | None = None
    macro_replay_mouse_movement: bool = True
    macro_replay_mouse_clicks: bool = True
    macro_speed: float = 1.0
    macro_loop_mode: str = "none"
    macro_loop_count: int = 1
    macro_loop_stop_behavior: str = DEFAULT_MACRO_LOOP_STOP_BEHAVIOR
    macro_move_to_start: bool = False
    macro_start_x: int = 0
    macro_start_y: int = 0
    macro_block_mouse_movement: bool = False
    macro_recording_slot: int = 0
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
    mpris_command: str | None = None
    move_x: int = 0
    move_y: int = 0
    axis_value: int = 0

    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = DEFAULT_RAPIDFIRE_HOLD_MS
    rapidfire_wait_ms: int = DEFAULT_RAPIDFIRE_WAIT_MS
    profile_deactivation: ProfileDeactivationPolicy | None = None

    def is_valid(self) -> bool:
        return self.action_type in SUPERKEY_ACTION_TYPES

    def __post_init__(self) -> None:
        self.output_id = normalize_gamepad_output_id(self.action_type, self.output_id)
        if self.action_type == ActionType.GAMEPAD_AXIS:
            self.target = normalize_gamepad_axis_target(self.target)
            self.axis_value = clamp_gamepad_axis_value(self.target, self.axis_value)
        rapidfire_enabled, rapidfire_hold_ms, rapidfire_wait_ms = normalize_rapidfire_fields(
            self.action_type,
            rapidfire_enabled=bool(self.rapidfire_enabled),
            rapidfire_hold_ms=int(self.rapidfire_hold_ms),
            rapidfire_wait_ms=int(self.rapidfire_wait_ms),
        )
        self.rapidfire_enabled = rapidfire_enabled
        self.rapidfire_hold_ms = rapidfire_hold_ms
        self.rapidfire_wait_ms = rapidfire_wait_ms
        self.profile_deactivation = normalize_profile_deactivation_policy(
            self.action_type,
            self.profile_deactivation,
        )
        if self.action_type == ActionType.MPRIS:
            self.mpris_command = normalize_mpris_command(self.mpris_command)
        else:
            self.mpris_command = None
        if self.action_type in {
            ActionType.START_MACRO_RECORDING,
            ActionType.STOP_MACRO_RECORDING,
            ActionType.PLAY_MACRO_SLOT,
        }:
            self.macro_recording_slot = normalize_macro_recording_slot(self.macro_recording_slot)
        else:
            self.macro_recording_slot = 0


SUPERKEY_ACTION_SHARED_FIELDS = (
    "target",
    "output_id",
    "cmd",
    "exec_ref",
    "macro_name",
    "macro_replay_mouse_movement",
    "macro_replay_mouse_clicks",
    "macro_speed",
    "macro_loop_mode",
    "macro_loop_count",
    "macro_loop_stop_behavior",
    "macro_move_to_start",
    "macro_start_x",
    "macro_start_y",
    "macro_block_mouse_movement",
    "macro_recording_slot",
    "profile_name",
    "profile_deactivation",
    "compositor_id",
    "compositor_dispatcher",
    "compositor_args",
    "mpris_command",
    "move_x",
    "move_y",
    "axis_value",
    "rapidfire_enabled",
    "rapidfire_hold_ms",
    "rapidfire_wait_ms",
)


def superkey_action_shared_kwargs(action: object) -> dict[str, Any]:
    typed_action = cast(MappingAction | SuperkeyAction, action)
    kwargs = {
        field_name: getattr(typed_action, field_name)
        for field_name in SUPERKEY_ACTION_SHARED_FIELDS
    }
    rapidfire_enabled, rapidfire_hold_ms, rapidfire_wait_ms = normalize_rapidfire_fields(
        typed_action.action_type,
        rapidfire_enabled=bool(kwargs["rapidfire_enabled"]),
        rapidfire_hold_ms=int(kwargs["rapidfire_hold_ms"]),
        rapidfire_wait_ms=int(kwargs["rapidfire_wait_ms"]),
    )
    kwargs["rapidfire_enabled"] = rapidfire_enabled
    kwargs["rapidfire_hold_ms"] = rapidfire_hold_ms
    kwargs["rapidfire_wait_ms"] = rapidfire_wait_ms
    if typed_action.action_type == ActionType.MACRO:
        kwargs["macro_loop_stop_behavior"] = normalize_macro_loop_stop_behavior(
            kwargs["macro_loop_stop_behavior"]
        )
    return kwargs


def mapping_action_to_superkey_action(action: MappingAction) -> SuperkeyAction:
    if action.action_type not in SUPERKEY_ACTION_TYPES:
        raise ValueError(f"invalid pattern superkey action type: {action.action_type.value}")
    return SuperkeyAction(
        action_type=action.action_type,
        **superkey_action_shared_kwargs(action),
    )


def superkey_action_to_mapping_action(action: SuperkeyAction) -> MappingAction:
    return MappingAction(
        action_type=action.action_type,
        **superkey_action_shared_kwargs(action),
    )


@dataclass
class SuperkeyConfig:
    name: str
    description: str | None = None
    mode: SuperkeyMode = SuperkeyMode.PATTERN

    tap_actions: list[SuperkeyAction] = field(default_factory=list)
    double_tap_actions: list[SuperkeyAction] = field(default_factory=list)
    hold_actions: list[SuperkeyAction] = field(default_factory=list)
    tap_hold_actions: list[SuperkeyAction] = field(default_factory=list)
    overload_actions: list[MappingAction] = field(default_factory=list)
    overload_down_actions: list[MappingAction] = field(default_factory=list)
    overload_up_actions: list[MappingAction] = field(default_factory=list)

    tap_timeout_ms: int = 200
    double_tap_window_ms: int = 300
    hold_threshold_ms: int = 300

    def has_pattern_actions(self) -> bool:
        return any(
            (
                self.tap_actions,
                self.double_tap_actions,
                self.hold_actions,
                self.tap_hold_actions,
            )
        )

    def __post_init__(self) -> None:
        for actions in (
            self.tap_actions,
            self.double_tap_actions,
            self.hold_actions,
            self.tap_hold_actions,
        ):
            for action in actions:
                if action.action_type == ActionType.SUPERKEY:
                    raise ValueError("nested superkeys are not allowed inside superkeys")
        for action in (
            *self.overload_actions,
            *self.overload_down_actions,
            *self.overload_up_actions,
        ):
            if action.action_type == ActionType.SUPERKEY:
                raise ValueError("nested superkeys are not allowed inside superkeys")
            if action.action_type == ActionType.REPEAT:
                raise ValueError("repeat is not allowed inside overload superkeys")

    def has_overload_actions(self) -> bool:
        return bool(self.overload_actions or self.overload_down_actions or self.overload_up_actions)

    def has_any_action(self) -> bool:
        return self.has_pattern_actions() or self.has_overload_actions()


class _ComboCompatibleSuperkeyConfig(Protocol):
    mode: SuperkeyMode


@overload
def combo_effective_superkey_config(
    config: SuperkeyConfig,
    *,
    step_count: int,
) -> SuperkeyConfig: ...


@overload
def combo_effective_superkey_config(
    config: "RuntimeSuperkeyConfig",
    *,
    step_count: int,
) -> "RuntimeSuperkeyConfig": ...


def combo_effective_superkey_config[T: _ComboCompatibleSuperkeyConfig](
    config: T,
    *,
    step_count: int,
) -> T:
    if config.mode != SuperkeyMode.PATTERN or step_count <= 1:
        return config
    return cast(
        T,
        replace(
            cast(Any, config),
            double_tap_actions=[],
            tap_hold_actions=[],
        ),
    )


@dataclass
class WindowRule:
    field: str
    pattern: str


@dataclass
class DeviceProfileLayer:
    hardware_id: str
    always_grab_all: bool = False
    mappings: dict[str, MappingAction] = field(default_factory=dict)


@dataclass
class ComboEvent:
    evdev: str
    hardware_id: str = ""
    source: str | None = None


@dataclass
class ComboStep:
    events: list[ComboEvent] = field(default_factory=list)
    timeout_ms: int | None = None


@dataclass
class ComboConfig:
    id: str
    name: str = ""
    steps: list[ComboStep] = field(default_factory=list)
    action: MappingAction | None = None
    recall_trigger_keys: bool = False
    restore_trigger_keys: list[str] = field(default_factory=list)
    match_across_devices: bool = False


@dataclass
class ProfileConfig:
    name: str
    enabled: bool = True
    is_permanent: bool = False
    priority: int = 0
    notify_on_activation: bool = True
    activation_macro_name: str | None = None
    deactivation_macro_name: str | None = None
    window_rules: list[WindowRule] = field(default_factory=list)
    device_layers: dict[str, DeviceProfileLayer] = field(default_factory=dict)
    combos: list[ComboConfig] = field(default_factory=list)
    image: str | None = None
    created_at: datetime | None = None

    @property
    def state(self) -> ProfileState:
        if not self.enabled:
            return ProfileState.INACTIVE
        if self.is_permanent:
            return ProfileState.STANDBY
        if self.window_rules:
            return ProfileState.WAITING
        return ProfileState.INACTIVE

    def get_layer(self, hardware_id: str) -> DeviceProfileLayer | None:
        return self.device_layers.get(hardware_id)

    def ensure_layer(self, hardware_id: str) -> DeviceProfileLayer:
        layer = self.device_layers.get(hardware_id)
        if layer is None:
            layer = DeviceProfileLayer(hardware_id=hardware_id)
            self.device_layers[hardware_id] = layer
        return layer


@dataclass
class DeviceInfo:
    path: str
    name: str
    vendor_id: str
    product_id: str
    capabilities: list[str]
    device_type: DeviceType
