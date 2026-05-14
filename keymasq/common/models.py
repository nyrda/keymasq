from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast, overload

from keymasq.common.gamepad_axes import clamp_gamepad_axis_value, normalize_gamepad_axis_target

if TYPE_CHECKING:
    from keymasq.keymasqd.superkey_state import SuperkeyConfig as RuntimeSuperkeyConfig

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
    CANCEL_MACRO_PLAYBACK = "cancel_macro_playback"
    EMERGENCY_RESET = "emergency_reset"
    MACRO = "macro"
    MOUSE_MOVE_REL = "mouse_move_rel"
    MOUSE_MOVE_ABS = "mouse_move_abs"
    PROFILE_ENABLE = "profile_enable"
    PROFILE_DISABLE = "profile_disable"
    PROFILE_TOGGLE = "profile_toggle"


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
        ActionType.CANCEL_MACRO_PLAYBACK,
        ActionType.EMERGENCY_RESET,
        ActionType.MACRO,
        ActionType.PROFILE_ENABLE,
        ActionType.PROFILE_DISABLE,
        ActionType.PROFILE_TOGGLE,
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
    }
)

DEFAULT_RAPIDFIRE_HOLD_MS = 20
DEFAULT_RAPIDFIRE_WAIT_MS = 20
MIN_RAPIDFIRE_HOLD_MS = 0
MIN_RAPIDFIRE_WAIT_MS = 1

MACRO_LOOP_STOP_BEHAVIORS = frozenset({"finish_run", "cancel_run"})
DEFAULT_MACRO_LOOP_STOP_BEHAVIOR = "finish_run"


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
    int_value: Callable[[object, int], int],
) -> tuple[bool, int, int, bool]:
    return resolve_rapidfire_fields(
        action_type,
        rapidfire_enabled=bool(rapidfire_enabled),
        rapidfire_hold_ms=int_value(rapidfire_hold_ms, DEFAULT_RAPIDFIRE_HOLD_MS),
        rapidfire_wait_ms=int_value(rapidfire_wait_ms, DEFAULT_RAPIDFIRE_WAIT_MS),
    )


def normalize_macro_loop_stop_behavior(value: object) -> str:
    behavior = str(value or DEFAULT_MACRO_LOOP_STOP_BEHAVIOR).lower()
    if behavior in MACRO_LOOP_STOP_BEHAVIORS:
        return behavior
    return DEFAULT_MACRO_LOOP_STOP_BEHAVIOR


def normalize_gamepad_output_id(action_type: ActionType, output_id: object) -> str | None:
    if action_type not in (ActionType.GAMEPAD, ActionType.GAMEPAD_AXIS):
        return None
    if output_id is None:
        return None
    normalized = str(output_id).strip()
    return normalized or None


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
    analog_control_config: "AnalogControlConfig | None" = None
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
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
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


ANALOG_THRESHOLD_ACTION_TYPES = frozenset(
    action_type
    for action_type in ActionType
    if action_type
    not in {
        ActionType.PASSTHROUGH,
        ActionType.SUPPRESS,
        ActionType.ANALOG_CONTROL,
    }
)

ANALOG_MOUSE_CURVES = frozenset({"linear", "soft", "fast"})


def clamp_analog_value(value: object) -> float:
    return max(-1.0, min(1.0, float(cast(int | float | str | bytes, value))))


@dataclass
class AnalogMouseMotionConfig:
    enabled: bool = False
    speed: float = 900.0
    deadzone: float = 0.15
    curve: str = "soft"
    invert_x: bool = False
    invert_y: bool = False
    tick_ms: int = 8

    def __post_init__(self) -> None:
        self.speed = max(0.0, float(self.speed))
        self.deadzone = max(0.0, min(0.95, float(self.deadzone)))
        self.curve = str(self.curve or "soft").lower()
        if self.curve not in ANALOG_MOUSE_CURVES:
            self.curve = "soft"
        self.tick_ms = max(1, int(self.tick_ms))


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
    thresholds: list[AnalogActionThreshold] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.input_type = str(self.input_type or "stick").lower()
        validate_analog_control_config(self)


def validate_analog_control_config(config: AnalogControlConfig) -> None:
    if not str(config.name or "").strip():
        raise ValueError("analog control name is required")
    if config.input_type not in {"stick", "trigger"}:
        raise ValueError("analog control input_type must be 'stick' or 'trigger'")
    if config.input_type == "trigger" and config.mouse_motion.enabled:
        raise ValueError("trigger analog controls only support digital action ranges")
    for index, threshold in enumerate(config.thresholds, start=1):
        allowed_axes = {"x", "y"} if config.input_type == "stick" else {"x"}
        if threshold.axis not in allowed_axes:
            if config.input_type == "trigger":
                raise ValueError(f"threshold {index} axis must be 'x' for trigger controls")
            raise ValueError(f"threshold {index} axis must be 'x' or 'y'")
        if config.input_type == "trigger" and min(
            threshold.trigger_min,
            threshold.trigger_max,
            threshold.release_min,
            threshold.release_max,
        ) < 0.0:
            raise ValueError(f"threshold {index} trigger range values must be between 0 and 1")
        if threshold.trigger_min > threshold.trigger_max:
            raise ValueError(f"threshold {index} activation range is invalid")
        if threshold.release_min > threshold.release_max:
            raise ValueError(f"threshold {index} release range is invalid")
        if (
            threshold.trigger_min < threshold.release_min
            or threshold.trigger_max > threshold.release_max
        ):
            raise ValueError(
                f"threshold {index} activation range must be inside release range"
            )
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
    profile_name: str | None = None
    compositor_id: str | None = None
    compositor_dispatcher: str | None = None
    compositor_args: str | None = None
    move_x: int = 0
    move_y: int = 0
    axis_value: int = 0

    rapidfire_enabled: bool = False
    rapidfire_hold_ms: int = DEFAULT_RAPIDFIRE_HOLD_MS
    rapidfire_wait_ms: int = DEFAULT_RAPIDFIRE_WAIT_MS

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
    "profile_name",
    "compositor_id",
    "compositor_dispatcher",
    "compositor_args",
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

    def has_overload_actions(self) -> bool:
        return bool(
            self.overload_actions
            or self.overload_down_actions
            or self.overload_up_actions
        )

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
    hardware_id: str
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
